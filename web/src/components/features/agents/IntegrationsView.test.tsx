// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IntegrationsView } from './IntegrationsView';
import type { AgentIntegrationsApi } from '@/lib/api/agents';

vi.mock('@/lib/api/agents', () => ({
  getAgentIntegrations: vi.fn(),
  disconnectAgentIntegration: vi.fn(),
}));

import { disconnectAgentIntegration, getAgentIntegrations } from '@/lib/api/agents';

const mockedGetIntegrations = getAgentIntegrations as unknown as ReturnType<typeof vi.fn>;
const mockedDisconnect = disconnectAgentIntegration as unknown as ReturnType<typeof vi.fn>;

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
    expect(screen.getByRole('heading', { level: 2, name: 'MCP Servers' })).toBeInTheDocument();
    expect(
      await screen.findByText('Connected as HarvardMadSys · 2 repositories'),
    ).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('GitHub connected.');
    // GitLab is behind "Coming soon", so its connect_url is deliberately not
    // offered even though the API still returns one.
    expect(screen.queryByRole('link', { name: /Connect/ })).not.toBeInTheDocument();
    // Two: GitLab's, and the MCP Servers section's own long-standing one.
    expect(screen.getAllByText('Coming soon')).toHaveLength(2);
    expect(screen.getByText('Your own MCP servers')).toBeInTheDocument();
  });

  it('marks providers that need operator configuration', async () => {
    mockedGetIntegrations.mockResolvedValue({ providers: [] });

    render(<IntegrationsView />);

    // GitHub only: a provider that is not offered yet does not tell a user to
    // go and ask an admin for it.
    expect(await screen.findAllByText('Admin setup required')).toHaveLength(1);
    expect(screen.getAllByText('Coming soon')).toHaveLength(2);
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
    fireEvent.click(screen.getByText('Manage'));

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

  it('disconnects an account and refreshes provider state', async () => {
    mockedGetIntegrations.mockResolvedValueOnce(integrations).mockResolvedValueOnce({
      providers: [
        { ...integrations.providers[0], connected: false, accounts: [], repositories: [] },
        integrations.providers[1],
      ],
    });
    mockedDisconnect.mockResolvedValue(undefined);

    render(<IntegrationsView />);
    await screen.findByText('Connected as HarvardMadSys · 2 repositories');
    fireEvent.click(screen.getByText('Manage'));
    fireEvent.click(screen.getByRole('button', { name: 'Disconnect HarvardMadSys' }));

    await waitFor(() => expect(mockedDisconnect).toHaveBeenCalledWith('github', 'gh-42'));
    expect(await screen.findByRole('status')).toHaveTextContent('HarvardMadSys disconnected.');
    expect(mockedGetIntegrations).toHaveBeenCalledTimes(2);
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
    // One left: the MCP Servers pill. GitLab's is gone because it is connected.
    expect(screen.getAllByText('Coming soon')).toHaveLength(1);
  });
});
